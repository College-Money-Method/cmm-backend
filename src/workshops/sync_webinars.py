"""Airtable → DB sync for webinars (sessions) and their portal mappings."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.cycles.models import Cohort, Cycle
from src.integrations.airtable import get_webinar_records
from src.schools.models import School
from src.workshops.models import PortalMapping, Webinar, Workshop
from src.workshops.sync_utils import attachment_url, parse_airtable_datetime, select_stale_mapping_pairs

logger = logging.getLogger(__name__)

_WEBINAR_FIELD_MAP = [
    ("Video Embed Code", "video_embed_code"),
    ("StartURL", "start_url"),
    ("JoinURL", "join_url"),
    ("RegistrationURL", "registration_url"),
    ("Zoom Link", "zoom_link"),
    ("Webinar Name", "webinar_name"),
]

_WEBINAR_DT_FIELD_MAP = [
    ("Start Date and Time", "start_datetime"),
    ("End Date and Time", "end_datetime"),
]


def _sync_portal_mappings(
    db: Session,
    webinar_id,
    school_airtable_ids: list[str],
    school_by_airtable_id: dict,
    desired_pairs: set,
) -> None:
    """Create missing portal_mapping rows for a webinar's school list.

    Also records every resolvable (school_id, webinar_id) pair Airtable declares
    into ``desired_pairs`` so the post-loop reconciliation can delete any DB
    mapping that Airtable no longer lists (school removed from a webinar).
    """
    for at_id in school_airtable_ids:
        school = school_by_airtable_id.get(at_id)
        if not school:
            continue
        desired_pairs.add((school.id, webinar_id))
        # unique constraint on (school_id, webinar_id) prevents duplicates
        exists = db.execute(
            select(PortalMapping).where(
                PortalMapping.school_id == school.id,
                PortalMapping.webinar_id == webinar_id,
            )
        ).scalar_one_or_none()
        if not exists:
            db.add(PortalMapping(school_id=school.id, webinar_id=webinar_id))


def _reconcile_portal_mappings(
    db: Session,
    desired_pairs: set,
    processed_webinar_ids: set,
    max_missing_fraction: float,
) -> int:
    """Delete portal_mapping rows Airtable no longer lists (school un-assigned
    from a webinar), scoped to webinars seen in this pull.

    ``processed_webinar_ids`` contains only webinars whose Airtable "Schools"
    list is non-empty; webinars absent from the pull or with an empty list are
    never touched. Mirrors the contacts-sync partial-fetch guard: skips (logs
    error, deletes nothing) when the stale fraction exceeds
    ``max_missing_fraction`` — a spike signals a bad/partial Airtable pull rather
    than genuine removals. Returns the number of mappings removed.
    """
    if not processed_webinar_ids:
        return 0
    existing: list[PortalMapping] = db.execute(
        select(PortalMapping).where(PortalMapping.webinar_id.in_(processed_webinar_ids))
    ).scalars().all()
    if not existing:
        return 0

    pm_by_pair = {(pm.school_id, pm.webinar_id): pm for pm in existing}
    stale, guard_tripped = select_stale_mapping_pairs(
        list(pm_by_pair.keys()), desired_pairs, max_missing_fraction
    )
    if not stale:
        return 0
    if guard_tripped:
        logger.error(
            "Portal-mapping reconciliation SKIPPED: %d/%d (%.1f%%) mappings would be "
            "removed, exceeding max_missing_fraction=%.2f — treating as a bad/partial "
            "Airtable pull. No mappings deleted.",
            len(stale), len(existing), len(stale) / len(existing) * 100, max_missing_fraction,
        )
        return 0

    for pair in stale:
        pm = pm_by_pair[pair]
        logger.info(
            "Removing stale portal_mapping: school_id=%s webinar_id=%s "
            "(school no longer linked to this webinar in Airtable)",
            pm.school_id, pm.webinar_id,
        )
        db.delete(pm)
    return len(stale)


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

    # Cycle matched by name ("2024-2025", "2026-2027") from Airtable lookup field
    all_cycles: list[Cycle] = db.execute(select(Cycle)).scalars().all()
    cycle_by_name: dict[str, Cycle] = {c.name: c for c in all_cycles}

    # Cohort matched by airtable_id from Airtable linked field
    all_cohorts: list[Cohort] = db.execute(select(Cohort)).scalars().all()
    cohort_by_airtable_id: dict[str, Cohort] = {c.airtable_id: c for c in all_cohorts if c.airtable_id}

    # School matched by airtable_id for portal_mapping creation
    school_by_airtable_id: dict[str, School] = {s.airtable_id: s for s in db.execute(select(School)).scalars().all() if s.airtable_id}

    matched = updated = skipped = created = 0

    # (school_id, webinar_id) pairs Airtable declares this run, and the webinars
    # actually seen in the pull — both drive the post-loop mapping reconciliation.
    desired_pairs: set = set()
    processed_webinar_ids: set = set()

    for rec in records:
        fields = rec["fields"]
        airtable_rec_id: str = rec["id"]
        zoom_id: str | None = str(fields["Webinar ID"]) if fields.get("Webinar ID") is not None else None

        # Resolve cycle — "Name (from Cycle)" is a lookup field, returns a list in pyairtable
        cycle_names: list[str] = fields.get("Name (from Cycle)") or []
        cycle = cycle_by_name.get(cycle_names[0]) if cycle_names else None

        schools_linked: list[str] = fields.get("Schools") or []

        # Resolve cohort — "Cohort" is a linked field, returns a list of record IDs.
        # Some records have no Cohort link (single-school sessions): fall back to
        # the linked school's cohort so sessions still get one.
        cohort_ids: list[str] = fields.get("Cohort") or []
        cohort = cohort_by_airtable_id.get(cohort_ids[0]) if cohort_ids else None
        cohort_id = cohort.id if cohort else None
        if cohort_id is None:
            for school_at_id in schools_linked:
                school = school_by_airtable_id.get(school_at_id)
                if school and school.cohort_id:
                    cohort_id = school.cohort_id
                    break

        webinar = by_airtable_id.get(airtable_rec_id) or (by_zoom_id.get(zoom_id) if zoom_id else None)

        if not webinar:
            # Attempt to create — requires a resolvable workshop
            linked_workshops: list[str] = fields.get("Workshops") or []
            workshop_at_id = linked_workshops[0] if linked_workshops else None
            workshop = workshop_by_airtable_id.get(workshop_at_id) if workshop_at_id else None
            if not workshop:
                # Surface exactly why a new webinar could not be created so the
                # cause (empty link vs. unmatched workshop airtable_id) is visible.
                reason = (
                    "no 'Workshops' linked field on the Airtable record"
                    if not workshop_at_id
                    else f"linked workshop airtable_id={workshop_at_id!r} not found in DB "
                    "(workshop missing or its airtable_id/sequence_number not synced)"
                )
                logger.warning(
                    "Airtable webinar sync SKIPPED (no session created): "
                    "airtable_rec_id=%s zoom_webinar_id=%s webinar_name=%r — %s",
                    airtable_rec_id,
                    zoom_id,
                    fields.get("Webinar Name"),
                    reason,
                )
                skipped += 1
                continue

            webinar = Webinar(
                workshop_id=workshop.id,
                airtable_id=airtable_rec_id,
                zoom_webinar_id=zoom_id,
                webinar_name=fields.get("Webinar Name") or None,
                start_datetime=parse_airtable_datetime(fields.get("Start Date and Time")),
                end_datetime=parse_airtable_datetime(fields.get("End Date and Time")),
                join_url=fields.get("JoinURL") or None,
                start_url=fields.get("StartURL") or None,
                registration_url=fields.get("RegistrationURL") or None,
                video_embed_code=fields.get("Video Embed Code") or None,
                zoom_link=fields.get("Zoom Link") or None,
                audio_transcript=attachment_url(fields.get("Audio Transcript")),
                cycle_id=cycle.id if cycle else None,
                cohort_id=cohort_id,
            )
            db.add(webinar)
            db.flush()  # populate webinar.id
            # Only reconcile webinars whose Airtable "Schools" list is populated —
            # an empty list is ambiguous (template/unmanaged webinar or a lookup
            # glitch), so we never wipe existing mappings on it.
            if schools_linked:
                processed_webinar_ids.add(webinar.id)
            _sync_portal_mappings(db, webinar.id, schools_linked, school_by_airtable_id, desired_pairs)
            created += 1
            continue

        matched += 1
        changed = False

        if not webinar.airtable_id:
            webinar.airtable_id = airtable_rec_id
            changed = True

        # Backfill cycle on existing webinars that are missing it
        if cycle and webinar.cycle_id is None:
            webinar.cycle_id = cycle.id
            changed = True
        # Cohort follows Airtable (or the linked school's cohort) — update on change,
        # never clear when unresolved
        if cohort_id and webinar.cohort_id != cohort_id:
            webinar.cohort_id = cohort_id
            changed = True

        for at_field, db_col in _WEBINAR_FIELD_MAP:
            val: str | None = fields.get(at_field) or None
            if val and getattr(webinar, db_col) != val:
                setattr(webinar, db_col, val)
                changed = True

        for at_field, db_col in _WEBINAR_DT_FIELD_MAP:
            val = parse_airtable_datetime(fields.get(at_field))
            if val and getattr(webinar, db_col) != val:
                setattr(webinar, db_col, val)
                changed = True

        transcript_url = attachment_url(fields.get("Audio Transcript"))
        if transcript_url and webinar.audio_transcript != transcript_url:
            webinar.audio_transcript = transcript_url
            changed = True

        track = fields.get("Track Registrations")
        if track is not None:
            track_bool = bool(track) if isinstance(track, bool) else str(track).lower() == "true"
            if webinar.track_registrations != track_bool:
                webinar.track_registrations = track_bool
                changed = True

        if changed:
            updated += 1

        # Only reconcile when Airtable actually lists schools for this webinar
        # (see create-path note): empty list ≠ "remove all mappings".
        if schools_linked:
            processed_webinar_ids.add(webinar.id)
        _sync_portal_mappings(db, webinar.id, schools_linked, school_by_airtable_id, desired_pairs)

    # Remove school↔webinar mappings Airtable no longer lists (guarded).
    mappings_removed = _reconcile_portal_mappings(
        db, desired_pairs, processed_webinar_ids, settings.sync_deactivation_max_missing_fraction
    )

    db.flush()
    return {
        "matched": matched,
        "updated": updated,
        "skipped": skipped,
        "created": created,
        "mappings_removed": mappings_removed,
    }
