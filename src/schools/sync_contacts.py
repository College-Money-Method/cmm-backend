"""Airtable → DB sync for contacts (contacts table is the counselor source of truth).

Every Airtable contact is synced — including contacts with no school link
(valid data: counselors not yet in talk with a school → school_id NULL).
Business rule: a contact belongs to at most ONE school; the first entry of
the Airtable "Sch" link wins.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.integrations.airtable import get_contacts_records
from src.schools.models import Contact, School
from src.schools.sync_utils import parse_bool

logger = logging.getLogger(__name__)


def sync_contacts_from_airtable(db: Session) -> dict:
    """
    Upsert ALL Airtable contacts into the contacts table.

    - Dedup: airtable_id first, then lowercased email GLOBALLY (one contact =
      one email = one school; a school change must update, not duplicate)
    - school_id = first resolvable "Sch" link, else NULL (unlinked counselor)
    - Airtable is source of truth: names, email, role, comms flags, and
      school_id are updated on existing rows
    - Per-record errors don't abort the sync

    Returns {"contacts_created", "contacts_updated", "contacts_unlinked", "skipped"}
    """
    at_contacts = get_contacts_records()

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

    for crec in at_contacts:
        cfields = crec.get("fields", {})
        contact_airtable_id: str = crec["id"]
        email: str | None = (cfields.get("Email") or "").strip() or None

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

        new_values = {
            "school_id": school_id,
            "first_name": cfields.get("First Name") or None,
            "last_name": cfields.get("Last Name") or None,
            "email": email,
            "role": cfields.get("Role") or None,
            "receive_comms": parse_bool(cfields.get("Receive Comms")),
            "auto_emails": parse_bool(cfields.get("Auto Emails")),
            "softr_access": parse_bool(cfields.get("Softr Access")),
        }

        try:
            if existing:
                changed = False
                if not existing.airtable_id:
                    existing.airtable_id = contact_airtable_id
                    contact_by_airtable_id[contact_airtable_id] = existing
                    changed = True
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
                if email:
                    contact_by_email[email.lower()] = contact
                created += 1
                logger.info(
                    "Created contact: email=%s school=%s",
                    email or "(no email)", school.name if school else "(unlinked)",
                )
        except Exception as exc:
            logger.error(
                "Failed to sync contact %s (%s): %s",
                email or contact_airtable_id, contact_airtable_id, exc,
            )
            db.rollback()
            skipped += 1

    db.commit()
    logger.info(
        "Contacts sync complete: created=%d updated=%d unlinked=%d skipped=%d",
        created, updated, unlinked, skipped,
    )
    return {
        "contacts_created": created,
        "contacts_updated": updated,
        "contacts_unlinked": unlinked,
        "skipped": skipped,
    }
