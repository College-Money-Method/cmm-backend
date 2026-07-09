"""Airtable → DB sync for workshops: matching, rename detection, and creation.

Matching cascade per Airtable record (first hit wins):
  1. airtable_id          — fast path after first sync
  2. sequence_number      — first-run linkage via "Webinar Sequence"
  3. exact name           — first-run linkage for non-sequence workshops
  4. fuzzy name           — tolerates small wording/punctuation edits
  5. shared sessions      — the record's linked sessions already belong to one
                            DB workshop → the workshop was renamed in Airtable
  6. none                 — create a new workshop from the Airtable record
"""
from __future__ import annotations

import difflib
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.integrations.airtable import get_workshops_records
from src.workshops.models import Webinar, Workshop
from src.workshops.sync_utils import attachment_url

logger = logging.getLogger(__name__)

# Ratio for difflib.get_close_matches — high enough that distinct workshops with a
# shared suffix (e.g. "... with College Money Method") don't merge into each other.
_FUZZY_NAME_CUTOFF = 0.85

# Linked field on the Airtable Workshops table listing its session (webinar) records
_SESSIONS_LINK_FIELD = "Junction Table School Workshop 2"


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def _claimable(workshop: Workshop | None, airtable_rec_id: str) -> Workshop | None:
    """Reject a candidate already linked to a *different* Airtable record."""
    if workshop and workshop.airtable_id and workshop.airtable_id != airtable_rec_id:
        return None
    return workshop


def _match_by_fuzzy_name(norm_name: str, by_norm_name: dict[str, Workshop]) -> Workshop | None:
    close = difflib.get_close_matches(norm_name, by_norm_name.keys(), n=1, cutoff=_FUZZY_NAME_CUTOFF)
    return by_norm_name[close[0]] if close else None


def _match_by_shared_sessions(
    session_rec_ids: list[str],
    workshop_id_by_webinar_at_id: dict[str, object],
    workshop_by_id: dict[object, Workshop],
) -> Workshop | None:
    """Match a renamed workshop through its sessions.

    If the Airtable record's linked sessions already exist in the DB and all of
    them point at a single workshop, that workshop is the same entity under a
    new name. Ambiguous (multiple workshops) or unknown sessions → no match.
    """
    owner_ids = {
        workshop_id_by_webinar_at_id[rec_id]
        for rec_id in session_rec_ids
        if rec_id in workshop_id_by_webinar_at_id
    }
    if len(owner_ids) == 1:
        return workshop_by_id.get(owner_ids.pop())
    return None


def sync_workshops_from_airtable(db: Session) -> dict:
    """Pull all Airtable workshop records; update matched workshops, create missing ones.

    Stores airtable_id on first match. Updates name on matched workshops.
    Unmatched records become new Workshop rows populated from Airtable fields.
    """
    records = get_workshops_records()

    all_workshops: list[Workshop] = db.execute(select(Workshop)).scalars().all()
    by_airtable_id: dict[str, Workshop] = {w.airtable_id: w for w in all_workshops if w.airtable_id}
    by_sequence: dict[int, Workshop] = {w.sequence_number: w for w in all_workshops if w.sequence_number is not None}
    by_norm_name: dict[str, Workshop] = {_normalize_name(w.name): w for w in all_workshops if w.name}
    workshop_by_id: dict[object, Workshop] = {w.id: w for w in all_workshops}
    existing_slugs: set[str] = {w.resource_center_slug for w in all_workshops if w.resource_center_slug}

    # DB session (webinar) airtable_id → owning workshop_id, for rename detection
    workshop_id_by_webinar_at_id: dict[str, object] = {
        at_id: ws_id
        for at_id, ws_id in db.execute(
            select(Webinar.airtable_id, Webinar.workshop_id).where(Webinar.airtable_id.is_not(None))
        ).all()
    }

    matched = updated = skipped = created = 0

    for rec in records:
        fields = rec["fields"]
        airtable_rec_id: str = rec["id"]
        # "Webinar Sequence" is a singleLineText field in Airtable — tolerate non-numeric values
        raw_seq = fields.get("Webinar Sequence")
        try:
            seq: int | None = int(raw_seq) if raw_seq is not None else None
        except (TypeError, ValueError):
            seq = None

        name: str | None = (fields.get("Name") or "").strip() or None
        norm_name = _normalize_name(name) if name else None

        workshop = (
            by_airtable_id.get(airtable_rec_id)
            or _claimable(by_sequence.get(seq) if seq is not None else None, airtable_rec_id)
            or _claimable(by_norm_name.get(norm_name) if norm_name else None, airtable_rec_id)
            or _claimable(_match_by_fuzzy_name(norm_name, by_norm_name) if norm_name else None, airtable_rec_id)
            or _claimable(
                _match_by_shared_sessions(
                    fields.get(_SESSIONS_LINK_FIELD) or [], workshop_id_by_webinar_at_id, workshop_by_id
                ),
                airtable_rec_id,
            )
        )

        if not workshop:
            if not name:
                logger.warning("Airtable workshop sync SKIPPED: airtable_rec_id=%s has no Name", airtable_rec_id)
                skipped += 1
                continue

            # sequence_number is unique — a colliding sequence here means it belongs
            # to a workshop claimed by a different Airtable record
            if seq is not None and seq in by_sequence:
                logger.warning(
                    "New workshop %r: sequence_number=%s already taken — creating without sequence", name, seq
                )
                seq = None

            grades: list[str] = fields.get("Suggested Grades") or []
            slug: str | None = (fields.get("Resource Center Slug") or "").strip() or None
            if slug in existing_slugs:
                logger.warning(
                    "New workshop %r: resource_center_slug=%r already taken — creating without slug", name, slug
                )
                slug = None

            workshop = Workshop(
                name=name,
                sequence_number=seq,
                airtable_id=airtable_rec_id,
                description=fields.get("Description") or None,
                key_actions=fields.get("Workshop Key Actions") or None,
                suggested_grades=", ".join(grades) if grades else None,
                resource_center_slug=slug,
                workshop_art_url=attachment_url(fields.get("Workshop Art")),
            )
            db.add(workshop)
            # Register in lookups so a duplicate Airtable record cannot create a second row
            by_airtable_id[airtable_rec_id] = workshop
            by_norm_name[norm_name] = workshop
            if seq is not None:
                by_sequence[seq] = workshop
            if slug:
                existing_slugs.add(slug)
            created += 1
            logger.info("Created workshop from Airtable: name=%r airtable_id=%s sequence=%s", name, airtable_rec_id, seq)
            continue

        matched += 1
        changed = False

        if not workshop.airtable_id:
            workshop.airtable_id = airtable_rec_id
            changed = True

        if name and workshop.name != name:
            logger.info("Workshop renamed via Airtable: %r → %r (airtable_id=%s)", workshop.name, name, airtable_rec_id)
            by_norm_name.pop(_normalize_name(workshop.name), None)
            by_norm_name[norm_name] = workshop
            workshop.name = name
            changed = True

        if changed:
            updated += 1

    db.flush()
    return {"matched": matched, "updated": updated, "skipped": skipped, "created": created}
