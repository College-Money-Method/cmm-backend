"""Airtable → DB sync for contacts (contacts table is the counselor source of truth).

Every Airtable contact is synced — including contacts with no school link
(valid data: counselors not yet in talk with a school → school_id NULL).
Business rule: a contact belongs to at most ONE school; the first entry of
the Airtable "Sch" link wins.

Offboarding signals (Airtable is source of truth):
- Clearing a contact's "Sch" link → school_id NULL (access revoked by provisioning).
- Removing a contact entirely from Airtable → deleted_at set (soft-deactivate).
Both are reconciled into auth revocation by sync_provisioning.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.integrations.airtable import get_contacts_records
from src.schools.models import Contact, School
from src.schools.sync_utils import (
    deactivation_is_safe,
    detect_email_collisions,
    parse_bool,
    pick_collision_skip_ids,
)

logger = logging.getLogger(__name__)


def sync_contacts_from_airtable(db: Session) -> dict:
    """
    Upsert ALL Airtable contacts into the contacts table.

    - Dedup: airtable_id first, then lowercased email GLOBALLY (one contact =
      one email = one school; a school change must update, not duplicate)
    - Duplicate emails within one pull are processed first-occurrence-only (ISSUE-7)
    - Emptying a previously-set email is a no-op + warning (ISSUE-2)
    - Contacts removed entirely from Airtable are soft-deactivated via deleted_at,
      matched by airtable_id ONLY, guarded against partial fetches (ISSUE-3)
    - school_id = first resolvable "Sch" link, else NULL (unlinked counselor)
    - Airtable is source of truth: names, email, role, comms flags, and
      school_id are updated on existing rows
    - Per-record errors don't abort the sync

    Returns counts incl. contacts_deactivated, contacts_reactivated, email_collisions.
    """
    at_contacts = get_contacts_records()

    # ISSUE-7: duplicate emails in this pull — keep the SCHOOL-LINKED record
    # (the actual counselor), skip the rest. Falls back to first if none linked.
    email_collisions = detect_email_collisions(at_contacts)
    collision_skip_ids = pick_collision_skip_ids(at_contacts, email_collisions)
    for email, ids in email_collisions.items():
        logger.warning(
            "Duplicate email in Airtable pull: %s across records %s — keeping school-linked one",
            email, ids,
        )

    all_schools: list[School] = db.execute(select(School)).scalars().all()
    school_by_airtable_id: dict[str, School] = {s.airtable_id: s for s in all_schools if s.airtable_id}

    all_contacts: list[Contact] = db.execute(select(Contact)).scalars().all()
    contact_by_airtable_id: dict[str, Contact] = {c.airtable_id: c for c in all_contacts if c.airtable_id}
    # Email dedup is GLOBAL (not per school): a school change in Airtable must
    # update the existing row, not create a duplicate. When legacy duplicate
    # rows share an email, prefer the provisioned one (user_id set).
    contact_by_email: dict[str, Contact] = {}
    for c in all_contacts:
        if not (c.email and c.email.strip()):
            continue
        key = c.email.strip().lower()
        if key not in contact_by_email or (c.user_id and not contact_by_email[key].user_id):
            contact_by_email[key] = c

    created = updated = unlinked = skipped = 0
    collisions_skipped = reactivated = 0
    pulled_airtable_ids: set[str] = set()

    for crec in at_contacts:
        cfields = crec.get("fields", {})
        contact_airtable_id: str = crec["id"]
        pulled_airtable_ids.add(contact_airtable_id)
        email: str | None = (cfields.get("Email") or "").strip() or None

        # ISSUE-7: skip the non-winning records of a colliding email
        if contact_airtable_id in collision_skip_ids:
            logger.warning("Skipping duplicate-email contact %s (%s)", email, contact_airtable_id)
            collisions_skipped += 1
            continue

        # First school wins — contacts never belong to two schools
        school: School | None = None
        for sid in cfields.get("Sch") or []:
            school = school_by_airtable_id.get(sid)
            if school:
                break
        school_id = school.id if school else None
        if school_id is None:
            unlinked += 1

        existing = contact_by_airtable_id.get(contact_airtable_id)
        if not existing and email:
            existing = contact_by_email.get(email.lower())

        # ISSUE-2: don't blank a previously-set email — keep it and warn.
        # Offboarding is via the "Sch" link, not by clearing the email.
        effective_email = email
        if existing and email is None and existing.email:
            logger.warning(
                "Airtable contact %s has empty email; keeping existing '%s' (no-op)",
                contact_airtable_id, existing.email,
            )
            effective_email = existing.email

        new_values = {
            "school_id": school_id,
            "first_name": cfields.get("First Name") or None,
            "last_name": cfields.get("Last Name") or None,
            "email": effective_email,
            "role": cfields.get("Role") or None,
            "receive_comms": parse_bool(cfields.get("Receive Comms")),
            "auto_emails": parse_bool(cfields.get("Auto Emails")),
            "softr_access": parse_bool(cfields.get("Softr Access")),
        }

        try:
            # SAVEPOINT per record: a single failure can't roll back the batch.
            with db.begin_nested():
                if existing:
                    changed = False
                    # Backfill/refresh airtable_id: when matched by EMAIL and the
                    # pull's record id differs (Airtable deleted+recreated the
                    # record → new id), adopt the current id so this row isn't
                    # later mistaken for "removed from Airtable". Safe: if this id
                    # already owned another row we'd have matched that row by id.
                    if (
                        existing.airtable_id != contact_airtable_id
                        and contact_airtable_id not in contact_by_airtable_id
                    ):
                        old_aid = existing.airtable_id
                        existing.airtable_id = contact_airtable_id
                        contact_by_airtable_id[contact_airtable_id] = existing
                        changed = True
                        if old_aid:
                            logger.info(
                                "Refreshed airtable_id for %s: %s → %s",
                                effective_email or "(no email)", old_aid, contact_airtable_id,
                            )
                    # Reactivate: present in Airtable again after a prior soft-delete
                    if existing.deleted_at is not None:
                        existing.deleted_at = None
                        reactivated += 1
                        changed = True
                        logger.info(
                            "Reactivated contact: %s (%s)",
                            effective_email or "(no email)", contact_airtable_id,
                        )
                    for attr, value in new_values.items():
                        if getattr(existing, attr) != value:
                            setattr(existing, attr, value)
                            changed = True
                    if changed:
                        db.flush()
                        updated += 1
                else:
                    contact = Contact(airtable_id=contact_airtable_id, **new_values)
                    db.add(contact)
                    db.flush()
                    contact_by_airtable_id[contact_airtable_id] = contact
                    if effective_email:
                        contact_by_email[effective_email.lower()] = contact
                    created += 1
                    logger.info(
                        "Created contact: email=%s school=%s",
                        effective_email or "(no email)", school.name if school else "(unlinked)",
                    )
        except Exception as exc:
            logger.error(
                "Failed to sync contact %s (%s): %s",
                effective_email or contact_airtable_id, contact_airtable_id, exc,
            )
            skipped += 1

    # ISSUE-3 (contacts): soft-deactivate contacts removed entirely from Airtable.
    # Match by airtable_id ONLY (never email) to avoid human-error disasters.
    # Re-query from the DB so the guard denominator isn't skewed by any in-memory
    # entries from records that were rolled back this run.
    deactivated = 0
    db_contacts: list[Contact] = db.execute(
        select(Contact).where(Contact.airtable_id.isnot(None))
    ).scalars().all()
    contact_by_aid: dict[str, Contact] = {c.airtable_id: c for c in db_contacts}
    known_airtable_ids: set[str] = set(contact_by_aid.keys())
    if deactivation_is_safe(
        pulled_airtable_ids, known_airtable_ids, settings.sync_deactivation_max_missing_fraction
    ):
        now = datetime.now(timezone.utc)
        for aid in known_airtable_ids - pulled_airtable_ids:
            contact = contact_by_aid[aid]
            if contact.deleted_at is None:
                contact.deleted_at = now
                deactivated += 1
                logger.info(
                    "Deactivated contact removed from Airtable: %s (%s)",
                    contact.email or "(no email)", aid,
                )
    else:
        logger.error(
            "Skipping contact deactivation — Airtable pull looks partial (pulled=%d, known=%d)",
            len(pulled_airtable_ids), len(known_airtable_ids),
        )

    db.commit()
    logger.info(
        "Contacts sync complete: created=%d updated=%d unlinked=%d deactivated=%d "
        "reactivated=%d collisions_skipped=%d skipped=%d",
        created, updated, unlinked, deactivated, reactivated, collisions_skipped, skipped,
    )
    return {
        "contacts_created": created,
        "contacts_updated": updated,
        "contacts_unlinked": unlinked,
        "contacts_deactivated": deactivated,
        "contacts_reactivated": reactivated,
        "email_collisions": len(email_collisions),
        "skipped": skipped,
    }
