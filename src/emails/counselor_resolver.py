"""Resolve a school's counselor for merge-tag rendering.

A *counselor* is anyone who can log into the counselor hub — i.e. a Contact
with a provisioned login (``Contact.user_id`` set). The ``user_roles.role`` value
(hub_admin / hub_user / viewer) only governs *permissions*, not whether the
person is a counselor: they are all counselors of the hub. Login-less contacts
(``user_id`` NULL — the "no_access" case: families, un-provisioned Airtable
contacts) are NOT counselors.

Everything email-side that needs "who is this school's counselor" resolves it
here, so the definition lives in exactly one place.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.models import UserRole
from src.schools.models import Contact


def contact_is_school_counselor(contact: Contact | None, school_id: uuid.UUID | None) -> bool:
    """True when ``contact`` can log into the counselor hub for ``school_id`` —
    i.e. they ARE a counselor, so their own name should fill the counselor tags.

    The only signal is a provisioned hub login (``user_id`` set); the hub role
    is irrelevant. A ``school_id`` of None skips the school-membership check
    (used where the contact is already known to belong to the school)."""
    if contact is None or contact.user_id is None or not contact.is_active:
        return False
    return school_id is None or contact.school_id == school_id


def resolve_counselor_name(db: Session, school_id: uuid.UUID | None) -> tuple[str, str, str]:
    """Look up a representative counselor's name for the school. Returns
    ``(first, last, full)``, each "" when the school has no counselor on file.

    A school may have several counselors; for a school-level tag (no specific
    counselor in context) the director (``hub_admin``) is preferred, falling
    back to the earliest-provisioned counselor (any contact with a hub login)."""
    if school_id is None:
        return "", "", ""

    # Prefer the director (hub_admin) as the school's representative counselor.
    director_stmt = (
        select(Contact.first_name, Contact.last_name, Contact.full_name)
        .join(UserRole, UserRole.user_id == Contact.user_id)
        .where(
            UserRole.school_id == school_id,
            UserRole.role == "hub_admin",
            Contact.deleted_at.is_(None),
        )
        .order_by(Contact.created_at)
    )
    row = db.execute(director_stmt).first()

    # Otherwise any counselor of the school — a contact with a provisioned login.
    if row is None:
        any_counselor_stmt = (
            select(Contact.first_name, Contact.last_name, Contact.full_name)
            .where(
                Contact.school_id == school_id,
                Contact.user_id.is_not(None),
                Contact.deleted_at.is_(None),
            )
            .order_by(Contact.created_at)
        )
        row = db.execute(any_counselor_stmt).first()

    if row is None:
        return "", "", ""
    return row[0] or "", row[1] or "", row[2] or ""
