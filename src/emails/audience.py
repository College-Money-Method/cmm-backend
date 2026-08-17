"""Reusable audience resolver for broadcast (and future automated) sends.

``resolve_audience`` is the single place the 3 orthogonal filter dimensions
(school scope, role, opt-in) are combined into a SQLAlchemy query. School scope
is ALWAYS restricted server-side to customer schools
(``is_current_customer OR is_cmm_website_activated``) regardless of the
caller-supplied ``school_scope`` — defense in depth so a forged/stale
``school_id`` can never reach a non-customer school's contacts.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.models import UserRole
from src.schools.models import Contact, School


def resolve_audience(
    db: Session,
    school_scope: str,
    role_filter: str,
    opt_in_filter: str,
) -> list[Contact]:
    """Resolve the Contact rows matching the 3-dimension broadcast filter.

    Args:
        school_scope: ``"all_customers"`` for every customer school, or a
            school_id (str(uuid.UUID)) to scope to one specific school.
        role_filter: ``"all"`` for every contact role in scope, or
            ``"hub_admin"`` to restrict to hub_admin contacts only.
        opt_in_filter: ``"opted_in"`` (default, safe) restricts to
            ``Contact.auto_emails is True``. ``"all"`` removes that
            restriction — the ONLY dimension a caller may relax to reach
            non-opted-in contacts.

    Always excludes deactivated contacts (``deleted_at`` set) and contacts
    with no email address — there is nothing to send those either way.
    """
    is_customer_school = School.is_current_customer.is_(True) | School.is_cmm_website_activated.is_(True)

    stmt = (
        select(Contact)
        .join(School, Contact.school_id == School.id)
        .where(is_customer_school, Contact.deleted_at.is_(None), Contact.email.is_not(None))
    )

    if school_scope != "all_customers":
        try:
            school_id = uuid.UUID(school_scope)
        except (ValueError, AttributeError, TypeError):
            # Not a real school_id and not "all_customers" -> matches nothing,
            # rather than raising, so a bad param degrades to an empty audience.
            return []
        stmt = stmt.where(School.id == school_id)

    if role_filter == "hub_admin":
        # "hub_admin" is an app role in user_roles (scoped by school), NOT
        # Contact.role (which holds the Airtable job title Director/Counselor).
        stmt = stmt.join(
            UserRole,
            (UserRole.user_id == Contact.user_id) & (UserRole.school_id == Contact.school_id),
        ).where(UserRole.role == "hub_admin")

    if opt_in_filter != "all":
        stmt = stmt.where(Contact.auto_emails.is_(True))

    return list(db.scalars(stmt).all())


def resolve_contacts_by_ids(db: Session, contact_ids: list[uuid.UUID]) -> list[Contact]:
    """Resolve an explicit, admin-edited recipient set to Contact rows.

    Applies the same non-negotiable server-side guards as ``resolve_audience``
    (customer school only, not deactivated, has an email) so a forged or stale
    id can never reach a non-customer school's contacts — but does NOT apply the
    opt-in filter: the admin has explicitly chosen these recipients. Unsubscribe
    suppression is still enforced downstream at send time.
    """
    if not contact_ids:
        return []
    is_customer_school = School.is_current_customer.is_(True) | School.is_cmm_website_activated.is_(True)
    stmt = (
        select(Contact)
        .join(School, Contact.school_id == School.id)
        .where(
            Contact.id.in_(contact_ids),
            is_customer_school,
            Contact.deleted_at.is_(None),
            Contact.email.is_not(None),
        )
    )
    return list(db.scalars(stmt).all())
