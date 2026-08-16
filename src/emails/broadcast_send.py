"""Merge-tag resolution and send/send-test logic for broadcasts.

Kept separate from ``broadcast_router.py`` so the router stays a thin HTTP
layer; this module owns the actual fan-out to ``ses_client.send_email`` and
the per-recipient merge tag substitution.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.deps import get_db
from src.emails.broadcast_models import Broadcast
from src.emails.counselor_resolver import contact_is_school_counselor, resolve_counselor_name
from src.emails.renderer import render_email
from src.emails.ses_client import _sandbox_enabled, send_email
from src.emails.unsubscribe import build_unsubscribe_url
from src.schools.models import Contact, School

logger = logging.getLogger(__name__)

# Placeholder merge tags for a context-less test send (no sample contact to
# render against). Keys mirror ``build_merge_tag_replacements``.
_EMPTY_MERGE_TAGS = {
    "school_name": "",
    "counselor_name": "",
    "counselor_first_name": "",
    "counselor_last_name": "",
    "family_label": "families",
    "resource_center_url": "",
    "resource_center_password": "",
}


def build_merge_tag_replacements(school: School | None) -> dict[str, str]:
    """Build the school-level ``{{tag}}`` -> value map, mirroring the frontend's
    ``COMM_MERGE_TAGS`` set (school_name, counselor_name, counselor_first_name,
    counselor_last_name, family_label, resource_center_url,
    resource_center_password).

    Counselor tags are seeded empty here and resolved by ``_merge_tags_for``
    (which has DB access) — they depend on the school's counselor (a contact with
    a hub login), not on any ``Contact.role``. ``counselor_name`` keeps its
    original key (the full name) for backward compatibility with existing
    templates.
    """
    school_name = school.name if school else ""
    resource_center_url = (school.school_resource_center_url if school else None) or ""
    resource_center_password = (school.cmm_website_password if school else None) or ""
    nickname = school.nickname if school else None
    family_label = nickname or (f"{school_name} families" if school_name else "families")

    return {
        "school_name": school_name,
        "counselor_name": "",
        "counselor_first_name": "",
        "counselor_last_name": "",
        "family_label": family_label,
        "resource_center_url": resource_center_url,
        "resource_center_password": resource_center_password,
    }


def _merge_tags_for(db: Session, contact: Contact | None, school: School | None) -> dict[str, str]:
    """Fill the counselor tags: when the ``contact`` is itself a counselor
    (can log into the hub), use their own name; otherwise look up the school's
    representative counselor. Empty strings when no counselor is on file."""
    replacements = build_merge_tag_replacements(school)
    school_id = school.id if school else None
    if contact is not None and contact_is_school_counselor(contact, school_id):
        replacements["counselor_first_name"] = contact.first_name or ""
        replacements["counselor_last_name"] = contact.last_name or ""
        replacements["counselor_name"] = contact.full_name or ""
    else:
        first, last, full = resolve_counselor_name(db, school_id)
        replacements["counselor_first_name"] = first
        replacements["counselor_last_name"] = last
        replacements["counselor_name"] = full
    return replacements


def send_to_contact(
    db: Session,
    broadcast: Broadcast,
    contact: Contact | None,
    school: School | None,
    override_to: str | None = None,
    sandbox_enabled: bool | None = None,
) -> None:
    """Render and send (or sandbox-log) the broadcast to a single contact.
    Never raises — a per-recipient failure is logged by ``send_email`` as a
    "failed" row and must not abort the rest of the batch.

    ``override_to`` redirects the send to a different address than the contact's
    own email (used by test sends to reach an admin who has no Contact row).
    ``contact`` may be None for a context-less test send — merge tags then
    render empty and no unsubscribe link is attached.
    ``sandbox_enabled`` is the once-per-batch sandbox decision; None (single
    sends) lets ``send_email`` read it from the DB.
    """
    to = override_to or (contact.email if contact else None)
    if not to:
        return
    replacements = _merge_tags_for(db, contact, school) if contact else _EMPTY_MERGE_TAGS
    # Only attach an unsubscribe link for a genuine subscriber send. On an
    # override (test) send the recipient differs from `contact`, so a link built
    # for the sample contact would let the tester unsubscribe a real family.
    unsubscribe_url = build_unsubscribe_url(contact.id) if contact and not override_to else None
    html, text = render_email(
        broadcast.body_json,
        replacements,
        broadcast.subject,
        school_slug=school.slug if school else None,
        unsubscribe_url=unsubscribe_url,
    )
    from src.emails.link_resolver import resolve_plain_text

    subject = resolve_plain_text(broadcast.subject, replacements)
    try:
        send_email(
            db,
            to=to,
            subject=subject,
            html=html,
            text=text,
            source="broadcast",
            broadcast_id=broadcast.id,
            unsubscribe_url=unsubscribe_url,
            sandbox_enabled=sandbox_enabled,
        )
    except Exception:  # noqa: BLE001 - a single recipient failure must not abort the batch
        logger.exception("Broadcast %s: send failed for a recipient", broadcast.id)


def send_broadcast_batch(broadcast_id: uuid.UUID, contact_ids: list[uuid.UUID]) -> None:
    """Background-task entry point: open a fresh DB session (never reuse the
    request-scoped session inside a background task) and send to every
    contact in the resolved audience snapshot.
    """
    for db in get_db():
        try:
            broadcast = db.get(Broadcast, broadcast_id)
            if broadcast is None:
                return
            contacts = list(
                db.scalars(select(Contact).where(Contact.id.in_(contact_ids))).all()
            )
            schools_by_id = {
                school.id: school
                for school in db.scalars(
                    select(School).where(
                        School.id.in_({c.school_id for c in contacts if c.school_id is not None})
                    )
                ).all()
            }
            # Resolve the sandbox flag ONCE for the whole batch — avoids a
            # per-recipient config query on a large fan-out.
            sandbox_enabled = _sandbox_enabled(db)
            for contact in contacts:
                school = schools_by_id.get(contact.school_id) if contact.school_id else None
                send_to_contact(db, broadcast, contact, school, sandbox_enabled=sandbox_enabled)

            broadcast.status = "sent"
            db.add(broadcast)
            db.commit()
        finally:
            pass  # get_db generator handles session close


def send_test(
    db: Session,
    broadcast: Broadcast,
    contact: Contact | None,
    override_to: str | None = None,
) -> None:
    """Send one immediate (non-background) test send. Reuses the request-scoped
    session since this is a synchronous, single-recipient call (no background
    task involved).

    ``contact`` supplies the merge-tag/school context (a sample audience
    contact) and may be None. ``override_to`` redirects the actual recipient —
    used when the requesting admin has no Contact row of their own.
    """
    school = db.get(School, contact.school_id) if contact and contact.school_id else None
    send_to_contact(db, broadcast, contact, school, override_to=override_to)
