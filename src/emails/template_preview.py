"""Merge-tag context assembly for template preview + send-test (super_admin).

Given a template category and a (school, webinar, contact) selection from the
preview dialog, build the same ``{{tag}}`` -> value replacements a real send
would produce, so the preview is faithful to what recipients get. Reuses the
broadcast (``_merge_tags_for``) and workshop (``build_workshop_merge_replacements``)
replacement builders rather than re-deriving tag values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.config import settings
from src.emails.audience import resolve_audience, resolve_contacts_by_ids
from src.emails.broadcast_send import _merge_tags_for
from src.emails.counselor_resolver import contact_is_school_counselor, resolve_counselor_name
from src.emails.workshop_merge_tags import build_workshop_merge_replacements
from src.schools.models import Contact, School
from src.workshops.models import Webinar, Workshop
from src.workshops.registration_counts import school_registration_counts


@dataclass
class PreviewContext:
    """Resolved rendering inputs: the merge-tag map plus the school slug used to
    scope internal links (both fed straight into ``renderer.render_email``)."""

    replacements: dict[str, str]
    school_slug: str | None
    # The addresses a grouped send would put on the To header ("Name <email>"),
    # so the preview can show who shares the one email. Empty for the normal
    # one-email-per-contact preview, which has no group to show.
    recipients: list[str] = field(default_factory=list)


def _load_school(db: Session, school_id: uuid.UUID) -> School:
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="School not found")
    return school


def _load_contact(db: Session, contact_id: uuid.UUID | None) -> Contact | None:
    if contact_id is None:
        return None
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    return contact


def _recipient_label(contact: Contact) -> str:
    """`"Name <email>"`, or the bare address when the contact has no name."""
    name = (contact.full_name or "").strip()
    return f"{name} <{contact.email}>" if name else (contact.email or "")


def _grouped_recipients(
    db: Session,
    school: School,
    *,
    role_filter: str,
    opt_in_filter: str,
    recipient_contact_ids: list[uuid.UUID] | None,
) -> list[Contact]:
    """The contacts one grouped email to ``school`` would be addressed to.

    Resolved through the same resolvers a real send uses: the admin-edited
    recipient set when there is one (narrowed to this school, since a preview
    renders one school's email at a time), otherwise the broadcast's audience
    filters applied to this school alone.
    """
    if recipient_contact_ids:
        return [
            c
            for c in resolve_contacts_by_ids(db, recipient_contact_ids)
            if c.school_id == school.id
        ]
    return resolve_audience(db, [str(school.id)], None, role_filter, opt_in_filter)


def build_preview_context(
    db: Session,
    *,
    category: str,
    school_id: uuid.UUID,
    webinar_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    grouped: bool = False,
    role_filter: str = "all",
    opt_in_filter: str = "opted_in",
    recipient_contact_ids: list[uuid.UUID] | None = None,
) -> PreviewContext:
    """Resolve the merge-tag context for a preview/test render.

    ``general`` needs only a school (``counselor_name``/``family_label`` come
    from the school's representative counselor — a chosen ``contact`` overrides
    ``counselor_name`` when they ARE a counselor, i.e. can log into the hub).
    ``workshop`` additionally requires a webinar to fill the
    date/time/workshop tags.

    ``grouped`` previews the "one email per school" broadcast option: the whole
    school audience is passed to the same merge-tag builder a grouped send uses,
    so ``recipient_first_names`` shows the real joined greeting ("Hi Paul,
    Caroline and Vu,") instead of the single sample contact's name. The sample
    contact is ignored in that mode, exactly as a grouped send ignores any one
    recipient. Grouping is a broadcast-only option, so it does not apply to the
    workshop category.
    """
    school = _load_school(db, school_id)
    contact = _load_contact(db, contact_id)

    if category == "general":
        if grouped:
            recipients = _grouped_recipients(
                db,
                school,
                role_filter=role_filter,
                opt_in_filter=opt_in_filter,
                recipient_contact_ids=recipient_contact_ids,
            )
            return PreviewContext(
                _merge_tags_for(db, recipients, school),
                school.slug,
                [_recipient_label(c) for c in recipients],
            )
        return PreviewContext(_merge_tags_for(db, [contact] if contact else [], school), school.slug)

    # workshop
    if webinar_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="webinar_id is required for a workshop preview",
        )
    webinar = db.get(Webinar, webinar_id)
    if webinar is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webinar not found")
    workshop = db.get(Workshop, webinar.workshop_id)
    if workshop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workshop not found for webinar")

    family_label = school.nickname or (f"{school.name} families" if school.name else "families")
    if contact and contact.full_name and contact_is_school_counselor(contact, school.id):
        counselor_first = contact.first_name or ""
        counselor_last = contact.last_name or ""
        counselor_name = contact.full_name
    else:
        counselor_first, counselor_last, counselor_name = resolve_counselor_name(db, school.id)
    resources = [{"id": str(a.id), "name": a.name, "link": a.link} for a in workshop.content_assets]
    registration_count, attendee_count = school_registration_counts(db, webinar.id, school.id)
    replacements = build_workshop_merge_replacements(
        school_name=school.name,
        family_label=family_label,
        counselor_name=counselor_name,
        counselor_first_name=counselor_first,
        counselor_last_name=counselor_last,
        school_slug=school.slug,
        resource_center_password=school.cmm_website_password,
        workshop_name=workshop.name,
        webinar_id=webinar.id,
        start_datetime=webinar.start_datetime,
        suggested_grades=workshop.suggested_grades,
        cycle_name=webinar.cycle.name if webinar.cycle else None,
        registration_url=webinar.registration_url,
        resources=resources,
        origin=settings.app_public_url or None,
        registration_count=registration_count,
        attendee_count=attendee_count,
    )
    return PreviewContext(replacements, school.slug)
