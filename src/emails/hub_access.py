"""The Counselor Hub access requirement every CMM email recipient must meet.

A contact row exists for everyone a school lists, including staff who were
never given a hub login. Those people are not CMM's audience: the workshop
reminders and admin broadcasts all speak to a counselor who works inside the
Hub, and mailing a school's whole directory is how a contact list turns into
spam complaints.

"Hub access" here means exactly what the admin UI's "Hub Access" column shows —
a ``user_roles`` row provisioned for the contact's auth user (see
``Contact.hub_role``). Deliberately keyed on ``user_id`` alone, NOT additionally
on the school: an admin who sees "Hub Access: hub_user" on a contact must be
able to trust that contact is mailable, and a role whose ``school_id`` drifted
from the contact's would otherwise make the screen lie.

Both send paths import this one predicate — ``automation_runner`` for
scheduler-driven workshop mail and ``audience`` for broadcasts — so the rule
cannot be satisfied in one place and forgotten in the other.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement

from src.auth.models import UserRole
from src.schools.models import Contact


def has_hub_access() -> ColumnElement[bool]:
    """Correlated EXISTS: the contact under query has a provisioned hub role.

    Usable directly in any ``select(Contact)`` — it correlates to the enclosing
    ``contacts`` row. A contact with no ``user_id`` has never been provisioned,
    so the comparison yields NULL, the subquery matches nothing, and the
    predicate is false, which is the intended answer.

    ``correlate(Contact)`` is pinned rather than left to auto-correlation: the
    broadcast resolver's ``role_filter="hub_admin"`` branch joins ``user_roles``
    into the OUTER query too, and auto-correlation would then hoist this
    subquery's own ``user_roles`` out of it, leaving a FROM-less SELECT that
    SQLAlchemy refuses to compile.
    """
    return (
        select(UserRole.id)
        .where(UserRole.user_id == Contact.user_id)
        .correlate(Contact)
        .exists()
    )
