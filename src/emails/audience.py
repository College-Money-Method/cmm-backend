"""Reusable audience resolver for broadcast sends.

``resolve_audience`` is the single place the filter dimensions (school/cohort
targeting, role, opt-in) are combined into a SQLAlchemy query. School scope is
ALWAYS restricted server-side to current customers (``is_current_customer``)
regardless of the caller-supplied ids — defense in depth so a forged/stale
``school_id`` can never reach a prospect school's contacts.

Note that being emailable is stricter than being able to *see* the School
Resource Center: ``is_cmm_website_activated`` opens the SRC to a prospect for a
preview (see ``schools.router._find_public_school``) but deliberately does NOT
make that prospect's contacts addressable — a prospect never receives CMM mail
until they are a customer.

The opt-in dimension reads ``Contact.broadcast_emails``, the counselor-managed
opt-in for one-off admin broadcasts. Scheduler-driven workshop automations use
the separate ``Contact.auto_emails`` opt-in (see ``automation_runner``); the two
are independent by design.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.auth.models import UserRole
from src.schools.models import Contact, School


def _parse_uuids(values: list[str] | None) -> list[uuid.UUID]:
    """Keep the well-formed ids and silently drop the rest — a bad id degrades
    the audience rather than 500-ing a compose-time preview."""
    parsed: list[uuid.UUID] = []
    for value in values or []:
        try:
            parsed.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            continue
    return parsed


def resolve_audience(
    db: Session,
    school_ids: list[str] | None,
    cohort_ids: list[str] | None,
    role_filter: str,
    opt_in_filter: str,
) -> list[Contact]:
    """Resolve the Contact rows matching the broadcast filter.

    Args:
        school_ids: Explicitly targeted schools. Combined with ``cohort_ids`` as
            a UNION (a contact matches if its school is listed OR its school is
            in a listed cohort), so an admin can pick a cohort and then add
            individual schools on top.
        cohort_ids: Targeted cohorts, expanded to their member schools here.
        role_filter: ``"all"`` for every contact role in scope, or
            ``"hub_admin"`` to restrict to hub_admin contacts only.
        opt_in_filter: ``"opted_in"`` (default, safe) restricts to
            ``Contact.broadcast_emails is True``. ``"all"`` removes that
            restriction — the ONLY dimension a caller may relax to reach
            non-opted-in contacts.

    Both id lists empty means every current-customer school. Always excludes deactivated
    contacts (``deleted_at`` set) and contacts with no email address — there is
    nothing to send those either way.
    """
    stmt = (
        select(Contact)
        .join(School, Contact.school_id == School.id)
        .where(
            School.is_current_customer.is_(True),
            Contact.deleted_at.is_(None),
            Contact.email.is_not(None),
        )
    )

    parsed_school_ids = _parse_uuids(school_ids)
    parsed_cohort_ids = _parse_uuids(cohort_ids)
    if school_ids or cohort_ids:
        # The caller asked for a restriction; if none of the ids parsed, match
        # nothing rather than silently widening to every customer school.
        if not parsed_school_ids and not parsed_cohort_ids:
            return []
        scope_clauses = []
        if parsed_school_ids:
            scope_clauses.append(School.id.in_(parsed_school_ids))
        if parsed_cohort_ids:
            scope_clauses.append(School.cohort_id.in_(parsed_cohort_ids))
        stmt = stmt.where(or_(*scope_clauses))

    if role_filter == "hub_admin":
        # "hub_admin" is an app role in user_roles (scoped by school), NOT
        # Contact.role (which holds the Airtable job title Director/Counselor).
        stmt = stmt.join(
            UserRole,
            (UserRole.user_id == Contact.user_id) & (UserRole.school_id == Contact.school_id),
        ).where(UserRole.role == "hub_admin")

    if opt_in_filter != "all":
        stmt = stmt.where(Contact.broadcast_emails.is_(True))

    return list(db.scalars(stmt).all())


def resolve_contacts_by_ids(db: Session, contact_ids: list[uuid.UUID]) -> list[Contact]:
    """Resolve an explicit, admin-edited recipient set to Contact rows.

    Applies the same non-negotiable server-side guards as ``resolve_audience``
    (current-customer school only, not deactivated, has an email) so a forged or
    stale id can never reach a prospect school's contacts — but does NOT apply the
    opt-in filter: the admin has explicitly chosen these recipients. Unsubscribe
    suppression is still enforced downstream at send time.
    """
    if not contact_ids:
        return []
    stmt = (
        select(Contact)
        .join(School, Contact.school_id == School.id)
        .where(
            Contact.id.in_(contact_ids),
            School.is_current_customer.is_(True),
            Contact.deleted_at.is_(None),
            Contact.email.is_not(None),
        )
    )
    return list(db.scalars(stmt).all())
