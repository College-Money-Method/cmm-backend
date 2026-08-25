"""Merge-tag resolution and send/send-test logic for broadcasts.

Kept separate from ``broadcast_router.py`` so the router stays a thin HTTP
layer; this module owns the actual fan-out to ``ses_client.send_email`` and
the per-recipient merge tag substitution.

A broadcast sends either one email per contact (default) or, when
``group_by_school`` is set, one email per school addressed to every resolved
recipient at that school — multiple To: addresses, their first names joined into
``{{recipient_first_names}}`` ("Paul, Caroline and Vu").
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.deps import get_db
from src.emails.broadcast_models import Broadcast
from src.emails.counselor_resolver import contact_is_school_counselor, resolve_counselor_name
from src.emails.renderer import render_email
from src.emails.school_links import resource_center_url
from src.emails.sender import format_from_header
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
    "recipient_first_names": "",
    "family_label": "families",
    "resource_center_url": "",
    "resource_center_password": "",
}


def format_name_list(names: list[str]) -> str:
    """Join recipient first names the way a person would write them:
    "Paul", "Paul and Caroline", "Paul, Caroline and Vu".

    Blanks are dropped — a contact with no first name simply doesn't appear in
    the greeting rather than leaving a dangling comma.
    """
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return f"{', '.join(cleaned[:-1])} and {cleaned[-1]}"


def build_merge_tag_replacements(school: School | None) -> dict[str, str]:
    """Build the school-level ``{{tag}}`` -> value map, mirroring the frontend's
    ``COMM_MERGE_TAGS`` set (school_name, counselor_name, counselor_first_name,
    counselor_last_name, recipient_first_names, family_label,
    resource_center_url, resource_center_password).

    Counselor tags are seeded empty here and resolved by ``_merge_tags_for``
    (which has DB access) — they depend on the school's counselor (a contact with
    a hub login), not on any ``Contact.role``. ``counselor_name`` keeps its
    original key (the full name) for backward compatibility with existing
    templates. ``recipient_first_names`` is seeded empty and filled from the
    actual To: recipients by ``_merge_tags_for``.
    """
    # Imported lazily so tests can monkeypatch `settings.app_public_url` freely
    # without import-order surprises (same reason as renderer.py).
    from src.config import settings

    school_name = school.name if school else ""
    resource_center_password = (school.cmm_website_password if school else None) or ""
    nickname = school.nickname if school else None
    family_label = nickname or (f"{school_name} families" if school_name else "families")

    return {
        "school_name": school_name,
        "counselor_name": "",
        "counselor_first_name": "",
        "counselor_last_name": "",
        "recipient_first_names": "",
        "family_label": family_label,
        "resource_center_url": resource_center_url(
            settings.app_public_url, school.slug if school else None
        ),
        "resource_center_password": resource_center_password,
    }


def _merge_tags_for(db: Session, contacts: list[Contact], school: School | None) -> dict[str, str]:
    """Fill the counselor and recipient tags for one outgoing email.

    Counselor tags describe the *school's* representative counselor, except when
    the (single) recipient is themselves a counselor — then their own name is
    used. On a grouped send there is no single "you", so the school's counselor
    is used and the personal greeting belongs in ``recipient_first_names``.
    """
    replacements = build_merge_tag_replacements(school)
    school_id = school.id if school else None
    primary = contacts[0] if len(contacts) == 1 else None
    if primary is not None and contact_is_school_counselor(primary, school_id):
        replacements["counselor_first_name"] = primary.first_name or ""
        replacements["counselor_last_name"] = primary.last_name or ""
        replacements["counselor_name"] = primary.full_name or ""
    else:
        first, last, full = resolve_counselor_name(db, school_id)
        replacements["counselor_first_name"] = first
        replacements["counselor_last_name"] = last
        replacements["counselor_name"] = full
    replacements["recipient_first_names"] = format_name_list(
        [c.first_name or c.full_name or "" for c in contacts]
    )
    return replacements


def send_to_contacts(
    db: Session,
    broadcast: Broadcast,
    contacts: list[Contact],
    school: School | None,
    override_to: str | None = None,
    sandbox_enabled: bool | None = None,
) -> None:
    """Render and send (or sandbox-log) the broadcast as ONE email addressed to
    every contact in ``contacts``. Never raises — a per-email failure is logged by
    ``send_email`` as a "failed" row and must not abort the rest of the batch.

    ``override_to`` redirects the send to a different address than the contacts'
    own emails (used by test sends to reach an admin who has no Contact row).
    ``contacts`` may be empty for a context-less test send — merge tags then
    render empty and no unsubscribe link is attached.
    ``sandbox_enabled`` is the once-per-batch sandbox decision; None (single
    sends) lets ``send_email`` read it from the DB.
    """
    to: list[str] = [override_to] if override_to else [c.email for c in contacts if c.email]
    if not to:
        return
    replacements = _merge_tags_for(db, contacts, school) if contacts else dict(_EMPTY_MERGE_TAGS)
    # Only attach an unsubscribe link for a genuine subscriber send. On an
    # override (test) send the recipient differs from `contacts`, so a link built
    # for the sample contact would let the tester unsubscribe a real family.
    # A grouped email carries one token covering every recipient, so whoever
    # clicks can actually stop the mail they received.
    unsubscribe_url = (
        build_unsubscribe_url([c.id for c in contacts]) if contacts and not override_to else None
    )
    html, text = render_email(
        broadcast.body_json,
        replacements,
        broadcast.subject,
        school_slug=school.slug if school else None,
        unsubscribe_url=unsubscribe_url,
        include_branding=broadcast.include_branding,
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
            from_address=format_from_header(broadcast.sender_name, broadcast.sender_email),
        )
    except Exception:  # noqa: BLE001 - a single recipient failure must not abort the batch
        logger.exception("Broadcast %s: send failed for a recipient", broadcast.id)


def send_to_contact(
    db: Session,
    broadcast: Broadcast,
    contact: Contact | None,
    school: School | None,
    override_to: str | None = None,
    sandbox_enabled: bool | None = None,
) -> None:
    """Single-recipient view of ``send_to_contacts`` (test sends and ungrouped
    broadcasts)."""
    send_to_contacts(
        db,
        broadcast,
        [contact] if contact else [],
        school,
        override_to=override_to,
        sandbox_enabled=sandbox_enabled,
    )


def _group_by_school(contacts: list[Contact]) -> list[list[Contact]]:
    """One group per school, preserving the resolved order. Contacts with no
    school can't be grouped meaningfully, so each becomes its own group and is
    sent to individually."""
    groups: dict[uuid.UUID, list[Contact]] = defaultdict(list)
    ungrouped: list[list[Contact]] = []
    for contact in contacts:
        if contact.school_id is None:
            ungrouped.append([contact])
        else:
            groups[contact.school_id].append(contact)
    return list(groups.values()) + ungrouped


def send_broadcast_batch(broadcast_id: uuid.UUID, contact_ids: list[uuid.UUID]) -> None:
    """Background-task entry point: open a fresh DB session (never reuse the
    request-scoped session inside a background task) and send to every
    contact in the resolved audience snapshot — one email each, or one email per
    school when the broadcast is grouped.
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
            groups = (
                _group_by_school(contacts) if broadcast.group_by_school else [[c] for c in contacts]
            )
            for group in groups:
                school_id = group[0].school_id
                school = schools_by_id.get(school_id) if school_id else None
                send_to_contacts(db, broadcast, group, school, sandbox_enabled=sandbox_enabled)

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
