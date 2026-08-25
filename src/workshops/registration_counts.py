"""School-scoped registration/attendance counts for one webinar.

The workshop portal already reports these two numbers per school (see
``workshops.router``'s portal detail); this module is the shared query behind
the ``registrations_to_date`` / ``attendees`` email merge tags, so a counselor
reading an email sees the same figures as the Hub.
"""

from __future__ import annotations

import uuid

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from src.workshops.models import WorkshopRegistration


def school_registration_counts(
    db: Session, webinar_id: uuid.UUID, school_id: uuid.UUID | None
) -> tuple[int, int]:
    """``(registrations, attendees)`` for ``school_id`` at ``webinar_id``.

    Counted live rather than stored, so "to date" means "as of this render" —
    the number an email quotes is true at send time. A school with no
    registrations (or an unknown school) counts as ``(0, 0)``.
    """
    if school_id is None:
        return 0, 0
    registrations, attendees = db.execute(
        select(
            func.count(WorkshopRegistration.id),
            func.coalesce(
                func.sum(case((WorkshopRegistration.attended.is_(True), 1), else_=0)), 0
            ),
        ).where(
            WorkshopRegistration.webinar_id == webinar_id,
            WorkshopRegistration.school_id == school_id,
        )
    ).one()
    return int(registrations or 0), int(attendees or 0)
